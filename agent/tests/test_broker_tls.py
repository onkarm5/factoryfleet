"""Certificate configuration and the TLS handshake setup it drives."""

from pathlib import Path

import pytest

from factoryfleet_agent import config as config_module
from factoryfleet_agent.config import BrokerConfig
from factoryfleet_agent.transport import broker as broker_module

AGENT_TOML = """
[asset]
site_id = "PLANT-A"
asset_id = "PRESS-01"
machine_type = "HYDRAULIC_PRESS"
firmware_version = "2.4.1"

[broker]
host = "a1b2c3-ats.iot.ap-south-1.amazonaws.com"
port = 8883
tls = {tls}
keepalive_seconds = 30
client_id = ""
{certificates}

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
noise_mm_s = 0.3
peak_factor = 2.5
hard_limit_rms_mm_s = 15.0
"""


def write_config(root: Path, *, tls="true", certificates="", make_files=True) -> Path:
    config_dir = root / "config"
    (config_dir / "sensors").mkdir(parents=True)
    (config_dir / "sensors" / "vibration.toml").write_text(SENSOR_TOML)

    if make_files:
        certs = root / "certs" / "PRESS-01"
        certs.mkdir(parents=True, exist_ok=True)
        (certs / "certificate.crt").write_text("-- certificate --")
        (certs / "private.key").write_text("-- key --")

    (config_dir / "agent.toml").write_text(
        AGENT_TOML.format(tls=tls, certificates=certificates)
    )
    return config_dir


IOT_CERTIFICATES = """
client_cert_path = "certs/PRESS-01/certificate.crt"
client_key_path = "certs/PRESS-01/private.key"
"""


class FakeMqttClient:
    """Captures what the real paho client would have been told."""

    def __init__(self, *_args, **kwargs):
        self.init_kwargs = kwargs
        self.tls_kwargs: dict | None = None
        self.on_connect = None
        self.on_disconnect = None

    def tls_set(self, **kwargs):
        self.tls_kwargs = kwargs

    def reconnect_delay_set(self, **_kwargs):
        pass


@pytest.fixture
def fake_client(monkeypatch):
    created: list[FakeMqttClient] = []

    def factory(*args, **kwargs):
        client = FakeMqttClient(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(broker_module.mqtt, "Client", factory)
    return created


def iot_broker_config(tmp_path, **overrides) -> BrokerConfig:
    defaults = dict(
        host="a1b2c3-ats.iot.ap-south-1.amazonaws.com",
        port=8883,
        tls=True,
        keepalive_seconds=30,
        client_id="PRESS-01",
        client_cert_path=tmp_path / "certificate.crt",
        client_key_path=tmp_path / "private.key",
    )
    return BrokerConfig(**{**defaults, **overrides})


# --- configuration --------------------------------------------------------------------


def test_certificate_paths_resolve_against_the_agent_directory(tmp_path):
    loaded = config_module.load(write_config(tmp_path, certificates=IOT_CERTIFICATES))

    assert loaded.broker.client_cert_path == tmp_path / "certs/PRESS-01/certificate.crt"
    assert loaded.broker.client_key_path == tmp_path / "certs/PRESS-01/private.key"
    assert loaded.broker.mutual_tls is True


def test_absolute_certificate_paths_are_left_alone(tmp_path):
    certs = tmp_path / "certs" / "PRESS-01"
    absolute = f"""
client_cert_path = "{certs / 'certificate.crt'}"
client_key_path = "{certs / 'private.key'}"
"""

    loaded = config_module.load(write_config(tmp_path, certificates=absolute))

    assert loaded.broker.client_cert_path == certs / "certificate.crt"


def test_an_unset_ca_path_means_the_system_trust_store(tmp_path):
    """IoT Core chains to Amazon Root CA 1, which every current CA bundle already has."""
    loaded = config_module.load(write_config(tmp_path, certificates=IOT_CERTIFICATES))

    assert loaded.broker.ca_cert_path is None


def test_a_private_ca_can_be_supplied(tmp_path):
    (tmp_path / "certs" / "PRESS-01").mkdir(parents=True)
    (tmp_path / "certs" / "PRESS-01" / "ca.crt").write_text("-- ca --")
    certificates = IOT_CERTIFICATES + '\nca_cert_path = "certs/PRESS-01/ca.crt"\n'

    loaded = config_module.load(write_config(tmp_path, certificates=certificates, make_files=True))

    assert loaded.broker.ca_cert_path == tmp_path / "certs/PRESS-01/ca.crt"


def test_tls_without_a_client_certificate_is_rejected(tmp_path):
    """The fleet has no passwords; a certificate is the only way a machine identifies itself."""
    with pytest.raises(config_module.ConfigError, match="client_cert_path"):
        config_module.load(write_config(tmp_path, certificates=""))


def test_tls_with_only_a_certificate_and_no_key_is_rejected(tmp_path):
    certificates = '\nclient_cert_path = "certs/PRESS-01/certificate.crt"\n'

    with pytest.raises(config_module.ConfigError, match="client_key_path"):
        config_module.load(write_config(tmp_path, certificates=certificates))


def test_a_missing_certificate_file_stops_startup_and_names_the_path(tmp_path):
    """paho would report this from its network thread as a connection that never succeeds.

    A machine that silently never connects is the hardest failure to spot on a monitoring
    system, so this has to fail loudly at startup instead.
    """
    with pytest.raises(config_module.ConfigError, match="does not exist"):
        config_module.load(
            write_config(tmp_path, certificates=IOT_CERTIFICATES, make_files=False)
        )


def test_certificate_paths_are_ignored_when_tls_is_off(tmp_path):
    """Switching a machine to IoT Core should be one boolean, not a config rewrite."""
    loaded = config_module.load(
        write_config(tmp_path, tls="false", certificates=IOT_CERTIFICATES, make_files=False)
    )

    assert loaded.broker.tls is False
    assert loaded.broker.client_cert_path == tmp_path / "certs/PRESS-01/certificate.crt"


def test_a_non_string_certificate_path_is_rejected(tmp_path):
    with pytest.raises(config_module.ConfigError, match="must be a string path"):
        config_module.load(write_config(tmp_path, certificates="\nclient_cert_path = 42\n"))


def test_the_client_id_still_defaults_to_the_asset_id_over_tls(tmp_path):
    """IoT Core policies pin the client id, so a stolen certificate cannot connect as another."""
    loaded = config_module.load(write_config(tmp_path, certificates=IOT_CERTIFICATES))

    assert loaded.broker.client_id == "PRESS-01"


# --- handshake setup ------------------------------------------------------------------


def test_the_client_certificate_is_presented_to_the_broker(tmp_path, fake_client):
    config = iot_broker_config(tmp_path)

    broker_module.MqttBrokerClient(config)

    tls = fake_client[0].tls_kwargs
    assert tls["certfile"] == str(tmp_path / "certificate.crt")
    assert tls["keyfile"] == str(tmp_path / "private.key")


def test_the_brokers_own_certificate_is_verified(tmp_path, fake_client):
    """Skipping verification would make the encrypted channel worthless against interception."""
    import ssl

    broker_module.MqttBrokerClient(iot_broker_config(tmp_path))

    assert fake_client[0].tls_kwargs["cert_reqs"] == ssl.CERT_REQUIRED


def test_no_ca_file_is_passed_when_using_the_system_trust_store(tmp_path, fake_client):
    broker_module.MqttBrokerClient(iot_broker_config(tmp_path))

    assert fake_client[0].tls_kwargs["ca_certs"] is None


def test_a_private_ca_is_passed_through(tmp_path, fake_client):
    config = iot_broker_config(tmp_path, ca_cert_path=tmp_path / "ca.crt")

    broker_module.MqttBrokerClient(config)

    assert fake_client[0].tls_kwargs["ca_certs"] == str(tmp_path / "ca.crt")


def test_no_tls_is_configured_for_a_plaintext_broker(tmp_path, fake_client):
    config = BrokerConfig(
        host="localhost", port=1883, tls=False, keepalive_seconds=30, client_id="PRESS-01"
    )

    broker_module.MqttBrokerClient(config)

    assert fake_client[0].tls_kwargs is None


def test_the_connection_is_made_under_the_asset_id(tmp_path, fake_client):
    broker_module.MqttBrokerClient(iot_broker_config(tmp_path))

    assert fake_client[0].init_kwargs["client_id"] == "PRESS-01"
