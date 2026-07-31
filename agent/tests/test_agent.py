import time
from pathlib import Path

import pytest

from factoryfleet_agent import config as config_module
from factoryfleet_agent.agent import Agent
from factoryfleet_agent.main import main, parse_args

REPO_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
PATIENCE = 5.0


class FakeBroker:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent: list[tuple[str, dict]] = []
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload):
        if not self.connected:
            return False
        self.sent.append((topic, payload))
        return True


def write_config(root: Path, *, sample_interval=0.02, publish_interval=0.02) -> Path:
    """A config directory pointing its database inside ``root``, sampling fast."""
    config_dir = root / "config"
    (config_dir / "sensors").mkdir(parents=True)
    (config_dir / "agent.toml").write_text(
        f"""
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
interval_seconds = {sample_interval}

[publishing]
interval_seconds = {publish_interval}
batch_size = 50

[storage]
database_path = "data/agent.sqlite"

[logging]
level = "INFO"
"""
    )
    for name, body in {
        "vibration": """
sensor_id = "vibration"
type = "vibration"
enabled = true

[options]
baseline_rms_mm_s = 4.2
noise_mm_s = 0.3
peak_factor = 2.5
hard_limit_rms_mm_s = 15.0
""",
        "temperature": """
sensor_id = "temperature"
type = "temperature"
enabled = true

[options]
baseline_bearing_c = 68.0
baseline_spindle_c = 55.0
noise_c = 1.2
hard_limit_bearing_c = 95.0
""",
    }.items():
        (config_dir / "sensors" / f"{name}.toml").write_text(body)
    return config_dir


@pytest.fixture
def config(tmp_path):
    return config_module.load(write_config(tmp_path))


def wait_for(predicate, patience=PATIENCE):
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- startup --------------------------------------------------------------------------


def test_starting_registers_the_asset_before_any_telemetry(config):
    """The backend should know what the machine is before readings about it arrive."""
    broker = FakeBroker()
    agent = Agent(config, client=broker)

    agent.start()
    try:
        assert wait_for(lambda: len(broker.sent) >= 2)
    finally:
        agent.stop()

    topics = [topic for topic, _payload in broker.sent]
    assert topics[0].endswith("/registration")
    assert "factoryfleet/PLANT-A/PRESS-01/telemetry" in topics


def test_registration_declares_the_machine_and_its_enabled_sensors(config):
    broker = FakeBroker()

    with Agent(config, client=broker, version="9.9.9"):
        assert wait_for(lambda: broker.sent)

    _topic, payload = broker.sent[0]
    assert payload["assetId"] == "PRESS-01"
    assert payload["machineType"] == "HYDRAULIC_PRESS"
    assert payload["agentVersion"] == "9.9.9"
    assert sorted(payload["sensors"]) == ["temperature", "vibration"]


def test_starting_opens_the_broker_connection(config):
    broker = FakeBroker()

    with Agent(config, client=broker):
        assert broker.started


def test_the_database_is_created_where_configuration_says(config):
    broker = FakeBroker()

    with Agent(config, client=broker):
        pass

    assert config.database_path.exists()


# --- the running agent ----------------------------------------------------------------


def test_telemetry_flows_while_running(config):
    broker = FakeBroker()

    with Agent(config, client=broker) as agent:
        assert wait_for(lambda: agent.runner.cycles >= 3)
        assert wait_for(lambda: telemetry_count(broker) >= 3)

    payload = next(p for topic, p in broker.sent if topic.endswith("/telemetry"))
    assert sorted(r["sensorId"] for r in payload["readings"]) == ["temperature", "vibration"]
    assert "batchedAt" in payload and "sentAt" in payload


def test_state_reflects_the_most_recent_reading_of_every_sensor(config):
    """What a get_status command will answer from, without waiting for the next sample."""
    broker = FakeBroker()

    with Agent(config, client=broker) as agent:
        assert wait_for(lambda: agent.runner.cycles >= 2)
        states = {state.sensor_id for state in agent.store.latest()}

    assert states == {"temperature", "vibration"}


def test_sampling_continues_while_the_broker_is_unreachable(config):
    """The reason the two loops are independent: an outage must not stop measurement."""
    broker = FakeBroker(connected=False)

    with Agent(config, client=broker) as agent:
        assert wait_for(lambda: agent.runner.cycles >= 3)

        assert broker.sent == []
        assert agent.store.pending_count() >= 3


def test_queued_telemetry_goes_out_when_the_broker_returns(config):
    broker = FakeBroker(connected=False)

    with Agent(config, client=broker) as agent:
        assert wait_for(lambda: agent.store.pending_count() >= 3)

        broker.connected = True

        assert wait_for(lambda: agent.store.pending_count() == 0)
        assert telemetry_count(broker) >= 3


# --- shutdown -------------------------------------------------------------------------


def test_stopping_halts_both_loops_and_closes_the_connection(config):
    broker = FakeBroker()
    agent = Agent(config, client=broker)

    agent.start()
    assert wait_for(lambda: agent.runner.cycles >= 1)
    agent.stop()

    assert not agent.is_running()
    assert broker.stopped
    cycles_at_stop = agent.runner.cycles
    time.sleep(0.1)
    assert agent.runner.cycles == cycles_at_stop, "the sampler kept running after stop"


def test_stopping_drains_what_the_broker_will_still_accept(config):
    """A clean shutdown should not leave readings on disk the broker was available to take."""
    broker = FakeBroker(connected=False)
    agent = Agent(config, client=broker)
    agent.start()
    # Cycles, not pending count: the queue also holds the registration entry.
    assert wait_for(lambda: agent.runner.cycles >= 2)

    broker.connected = True
    agent.stop()

    # Asserted on the broker rather than the store, which stop() has closed by now.
    assert telemetry_count(broker) >= 2


def test_stopping_twice_is_harmless(config):
    agent = Agent(config, client=FakeBroker())
    agent.start()

    agent.stop()
    agent.stop()

    assert not agent.is_running()


def test_a_failing_final_drain_does_not_prevent_shutdown(config, monkeypatch):
    """Refusing to exit is worse than a delay: unsent data is already durable."""
    agent = Agent(config, client=FakeBroker())
    agent.start()
    assert wait_for(lambda: agent.runner.cycles >= 1)

    monkeypatch.setattr(
        agent.publisher, "drain_once", lambda: (_ for _ in ()).throw(RuntimeError("disk full"))
    )
    agent.stop()

    assert not agent.is_running()


# --- restart --------------------------------------------------------------------------


def test_telemetry_buffered_through_an_outage_survives_a_restart(tmp_path):
    """The whole durability story in one test: outage, restart, nothing lost or reordered."""
    config = config_module.load(write_config(tmp_path))

    offline = FakeBroker(connected=False)
    first = Agent(config, client=offline)
    first.start()
    assert wait_for(lambda: first.store.pending_count() >= 3)
    buffered = first.store.pending_count()
    first.stop()

    assert offline.sent == []

    online = FakeBroker()
    second = Agent(config, client=online)
    second.start()
    try:
        assert wait_for(lambda: second.store.pending_count() == 0)
    finally:
        second.stop()

    # Everything buffered before the restart reached the broker afterwards.
    assert telemetry_count(online) >= buffered
    batched = [
        payload["batchedAt"]
        for topic, payload in online.sent
        if topic.endswith("/telemetry")
    ]
    assert batched == sorted(batched), "batches were reordered across the restart"


def test_a_restart_re_registers_the_asset(tmp_path):
    """Registration is idempotent, so re-declaring on restart updates rather than duplicates."""
    config = config_module.load(write_config(tmp_path))

    for _ in range(2):
        broker = FakeBroker()
        agent = Agent(config, client=broker)
        agent.start()
        try:
            assert wait_for(lambda: broker.sent)
        finally:
            agent.stop()
        assert broker.sent[0][0].endswith("/registration")


# --- command line ---------------------------------------------------------------------


def test_the_default_config_directory_is_the_one_in_the_repository():
    assert parse_args([]).config == REPO_CONFIG_DIR


def test_a_log_level_can_be_overridden_for_one_run():
    assert parse_args(["--log-level", "DEBUG"]).log_level == "DEBUG"


def test_a_bad_configuration_directory_exits_with_a_message_not_a_traceback(tmp_path, capsys):
    """The one line that matters is which file and key are wrong."""
    exit_code = main(["--config", str(tmp_path / "absent")])

    assert exit_code == 2
    assert "configuration error" in capsys.readouterr().err


def test_an_invalid_configuration_file_exits_with_a_message(tmp_path, capsys):
    config_dir = write_config(tmp_path)
    (config_dir / "agent.toml").write_text('[asset]\nsite_id = "PLANT-A"\n')

    exit_code = main(["--config", str(config_dir)])

    assert exit_code == 2
    assert "configuration error" in capsys.readouterr().err


def telemetry_count(broker):
    return sum(1 for topic, _payload in broker.sent if topic.endswith("/telemetry"))
